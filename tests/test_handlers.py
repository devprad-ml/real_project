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

from conftest import FAKE_PDFA, PAGE_TEXTS


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

        jobs = s.scalars(select(Job).where(Job.document_id == doc.id)).all()
        assert [j.kind for j in jobs] == [JobKind.EMBED]


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


# --- embed: OCR_DONE -> EMBEDDED ---------------------------------------------


def _seed_pages(doc, texts):
    """document_pages rows as `ocr` would have left them."""
    with SessionLocal() as s:
        for page_no, text in enumerate(texts, start=1):
            s.add(
                DocumentPages(
                    tenant_id=doc.tenant_id,
                    document_id=doc.id,
                    page_no=page_no,
                    ocr_text=text,
                )
            )
        s.commit()


def test_embed_upserts_chunks_and_flips_status(
    tenant, make_doc, vectors, fake_embedder
):
    doc = make_doc(status=DocumentStatus.OCR_DONE, page_count=2)
    _seed_pages(doc, ["first page of the denial", "second page of the denial"])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    points = vectors.points(f"chunks_{tenant}")
    assert len(points) == 2
    # Payload carries the text, so a RAG read needs one hit and no trip to Postgres.
    assert {p.payload["text"] for p in points} == {
        "first page of the denial",
        "second page of the denial",
    }
    assert {p.payload["page_no"] for p in points} == {1, 2}
    assert all(p.payload["tenant_id"] == str(tenant) for p in points)
    # Chunk-to-vector alignment: the fake encodes len(text), so a zip() that drifted
    # would attach the wrong page's vector.
    assert all(p.vector[0] == float(len(p.payload["text"])) for p in points)

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.EMBEDDED
        logs = s.scalars(select(Log).where(Log.document_id == doc.id)).all()
        assert [(log.detail["to"], log.detail["chunks"]) for log in logs] == [
            ("EMBEDDED", 2)
        ]


def test_embed_isolates_tenants_by_collection(
    tenant, make_doc, vectors, fake_embedder
):
    """The isolation boundary is the collection name, not a payload filter. Nothing
    may land in a collection belonging to another client."""
    doc = make_doc(status=DocumentStatus.OCR_DONE)
    _seed_pages(doc, ["protected health information"])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert list(vectors.collections) == [f"chunks_{tenant}"]


def test_embed_replay_overwrites_instead_of_duplicating(
    tenant, make_doc, vectors, fake_embedder
):
    """Deterministic point ids. A crash between the upsert and the commit replays the
    job: the second run must land on the same ids, not double every chunk."""
    doc = make_doc(status=DocumentStatus.OCR_DONE)
    _seed_pages(doc, ["a page of text to embed twice"])

    for _ in range(2):
        with SessionLocal() as s:
            fresh = s.get(Document, doc.id)
            fresh.status = DocumentStatus.OCR_DONE  # undo the flip, as a replay would
            handlers.handle_embed(s, fresh)
            s.commit()

    assert vectors.upserts == 2
    assert len(vectors.points(f"chunks_{tenant}")) == 1


def test_embed_is_a_noop_before_ocr_ran(tenant, make_doc, vectors, fake_embedder):
    doc = make_doc(status=DocumentStatus.NORMALIZED)

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert vectors.collections == {}
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NORMALIZED


def test_embed_is_a_noop_once_already_embedded(
    tenant, make_doc, vectors, fake_embedder
):
    doc = make_doc(status=DocumentStatus.EMBEDDED)
    _seed_pages(doc, ["already in the vector db"])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert vectors.upserts == 0


def test_a_fully_blank_document_embeds_zero_chunks_and_still_finishes(
    tenant, make_doc, vectors, fake_embedder
):
    """A fax of a blank sheet is a finished document, not a failure. Raising here
    would burn MAX_ATTEMPTS to reach the same answer and then mislabel it FAILED."""
    doc = make_doc(status=DocumentStatus.OCR_DONE, page_count=2)
    _seed_pages(doc, ["", "   "])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert vectors.upserts == 0  # no empty upsert call at all
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.EMBEDDED
        logs = s.scalars(select(Log).where(Log.document_id == doc.id)).all()
        assert logs[0].detail["chunks"] == 0


def test_a_failed_upsert_leaves_the_document_at_ocr_done(
    tenant, make_doc, vectors, fake_embedder, monkeypatch
):
    """Vectors before status. If the vector DB is down, the document must stay
    retryable -- EMBEDDED with no vectors behind it is invisible to every RAG query
    and nothing would ever notice."""
    doc = make_doc(status=DocumentStatus.OCR_DONE)
    _seed_pages(doc, ["a page the vector db will refuse"])

    def boom(collection, points):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(vectors, "upsert", boom)

    with SessionLocal() as s:
        with pytest.raises(RuntimeError):
            handlers.handle_embed(s, s.get(Document, doc.id))
        s.rollback()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.OCR_DONE
        assert s.scalars(select(Log).where(Log.document_id == doc.id)).all() == []
