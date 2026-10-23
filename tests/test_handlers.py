"""Handler transitions.

The OCR toolchain (Tesseract, Ghostscript), Claude and the embedding model are all
faked out here on purpose: these tests are about the state machine -- what status the
document lands in, what audit row is written, what job comes next, where the blob
ends up -- not about whether Tesseract can read a fax.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.enums import DocumentStatus, DocumentType, JobKind, JobState
from app.jobs import handlers
from app.models import Document, ExtractedFieldRow, Job, Log
from app.processing import classify, extract, pdftext

from conftest import FAKE_PDFA


def _jobs(s, doc):
    return s.scalars(select(Job).where(Job.document_id == doc.id)).all()


def _logs(s, doc):
    return s.scalars(select(Log).where(Log.document_id == doc.id)).all()


# --- normalize: RECEIVED -> NORMALIZED ----------------------------------------


def test_normalize_writes_unknown_pdf_and_enqueues_classify(
    tenant, make_doc, blob, fake_tools
):
    doc = make_doc()
    with SessionLocal() as s:
        handlers.handle_normalize(s, s.get(Document, doc.id))
        s.commit()

    # Not classified yet, so the file is named for the UNKNOWN type.
    expected_key = f"derived/{tenant}/{doc.id}/unknown.pdf"
    assert blob.data[expected_key] == FAKE_PDFA
    # The record is untouched -- normalize writes next to it, never over it.
    assert blob.data["raw/test/file.pdf"] == b"original record bytes"

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.NORMALIZED
        assert fresh.normalized_blob_key == expected_key
        assert fresh.page_count == 3

        jobs = _jobs(s, doc)
        assert [j.kind for j in jobs] == [JobKind.CLASSIFY]
        assert jobs[0].state == JobState.PENDING
        assert [log.detail["to"] for log in _logs(s, doc)] == ["NORMALIZED"]


def test_normalize_is_a_noop_once_past_received(tenant, make_doc, blob, fake_tools):
    """The replay guard. A job re-delivered after a crash must not enqueue a second
    classify job or rewrite the derived artifact."""
    doc = make_doc(status=DocumentStatus.NORMALIZED)
    with SessionLocal() as s:
        handlers.handle_normalize(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert _jobs(s, doc) == []
    assert f"derived/{tenant}/{doc.id}/unknown.pdf" not in blob.data


# --- classify: NORMALIZED -> CLASSIFIED ---------------------------------------


@pytest.fixture
def normalized_doc(tenant, make_doc, blob):
    """A document as normalize leaves it: NORMALIZED, PDF filed as unknown.pdf."""

    def _make():
        doc = make_doc(status=DocumentStatus.NORMALIZED, page_count=3)
        key = f"derived/{tenant}/{doc.id}/unknown.pdf"
        blob.data[key] = FAKE_PDFA
        with SessionLocal() as s:
            s.get(Document, doc.id).normalized_blob_key = key
            s.commit()
        return doc, key

    return _make


def test_classify_sets_the_type_renames_the_pdf_and_enqueues_embed(
    tenant, normalized_doc, blob, fake_tools, fake_classifier
):
    doc, old_key = normalized_doc()
    with SessionLocal() as s:
        handlers.handle_classify(s, s.get(Document, doc.id))
        s.commit()

    new_key = f"derived/{tenant}/{doc.id}/claims.pdf"
    assert blob.data[new_key] == FAKE_PDFA
    assert old_key not in blob.data  # deleted once the commit landed
    # Classify saw the page text, read from the PDF, in page order.
    assert fake_classifier.seen == ["page one text\npage two text\n"]

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.CLASSIFIED
        assert fresh.doc_type == DocumentType.CLAIMS
        assert fresh.classify_confidence == pytest.approx(0.93)
        assert fresh.normalized_blob_key == new_key
        assert [j.kind for j in _jobs(s, doc)] == [JobKind.EMBED]
        [log] = _logs(s, doc)
        assert log.detail == {
            "to": "CLASSIFIED", "doc_type": "claims", "confidence": 0.93
        }


def test_an_unknown_document_keeps_its_unknown_pdf(
    normalized_doc, blob, fake_tools, fake_classifier
):
    fake_classifier.result = classify.UNCLASSIFIED
    doc, key = normalized_doc()
    with SessionLocal() as s:
        handlers.handle_classify(s, s.get(Document, doc.id))
        s.commit()

    assert blob.data[key] == FAKE_PDFA
    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.CLASSIFIED
        assert fresh.normalized_blob_key == key


def test_a_rolled_back_classify_never_deletes_the_old_pdf(
    tenant, normalized_doc, blob, fake_tools, fake_classifier
):
    """The old key is deleted after commit, never before. If the transaction rolls
    back, the row still points at unknown.pdf, so unknown.pdf must still exist."""
    doc, old_key = normalized_doc()
    with SessionLocal() as s:
        handlers.handle_classify(s, s.get(Document, doc.id))
        s.rollback()

    assert blob.data[old_key] == FAKE_PDFA
    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.NORMALIZED
        assert fresh.normalized_blob_key == old_key
        # A later, unrelated commit on a fresh session must not replay the delete.
        s.commit()
    assert old_key in blob.data


def test_classify_is_a_noop_before_normalize_ran(
    make_doc, blob, fake_tools, fake_classifier
):
    doc = make_doc(status=DocumentStatus.RECEIVED)
    with SessionLocal() as s:
        handlers.handle_classify(s, s.get(Document, doc.id))
        s.commit()

    assert fake_classifier.seen == []
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.RECEIVED
        assert _jobs(s, doc) == []


# --- embed: CLASSIFIED -> EMBEDDED --------------------------------------------


@pytest.fixture
def classified_doc(tenant, make_doc, blob, monkeypatch):
    """A CLASSIFIED denial whose PDF holds `texts`, one string per page."""

    def _make(texts, status=DocumentStatus.CLASSIFIED):
        monkeypatch.setattr(pdftext, "pages", lambda pdf: enumerate(texts, start=1))
        doc = make_doc(status=status, page_count=len(texts))
        key = f"derived/{tenant}/{doc.id}/denial.pdf"
        blob.data[key] = FAKE_PDFA
        with SessionLocal() as s:
            fresh = s.get(Document, doc.id)
            fresh.normalized_blob_key = key
            fresh.doc_type = DocumentType.DENIAL
            s.commit()
        return doc

    return _make


def test_embed_upserts_chunks_and_flips_status(
    tenant, classified_doc, vectors, fake_embedder
):
    doc = classified_doc(["first page of the denial", "second page of the denial"])

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
    assert all(p.payload["doc_type"] == "denial" for p in points)
    # Chunk-to-vector alignment: the fake encodes len(text), so a zip() that drifted
    # would attach the wrong page's vector.
    assert all(p.vector[0] == float(len(p.payload["text"])) for p in points)
    # Every chunk also carries its keyword vector.
    assert all(p.sparse[0] and len(p.sparse[0]) == len(p.sparse[1]) for p in points)

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.EMBEDDED
        assert [(log.detail["to"], log.detail["chunks"]) for log in _logs(s, doc)] == [
            ("EMBEDDED", 2)
        ]


def test_embed_isolates_tenants_by_collection(
    tenant, classified_doc, vectors, fake_embedder
):
    """The isolation boundary is the collection name, not a payload filter. Nothing
    may land in a collection belonging to another client."""
    doc = classified_doc(["protected health information"])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert list(vectors.collections) == [f"chunks_{tenant}"]


def test_embed_replay_overwrites_instead_of_duplicating(
    tenant, classified_doc, vectors, fake_embedder
):
    """Deterministic point ids. A crash between the upsert and the commit replays the
    job: the second run must land on the same ids, not double every chunk."""
    doc = classified_doc(["a page of text to embed twice"])

    for _ in range(2):
        with SessionLocal() as s:
            fresh = s.get(Document, doc.id)
            fresh.status = DocumentStatus.CLASSIFIED  # undo the flip, as a replay would
            handlers.handle_embed(s, fresh)
            s.commit()

    assert vectors.upserts == 2
    assert len(vectors.points(f"chunks_{tenant}")) == 1


def test_embed_is_a_noop_before_classify_ran(
    classified_doc, vectors, fake_embedder
):
    doc = classified_doc(["not classified yet"], status=DocumentStatus.NORMALIZED)

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert vectors.collections == {}
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NORMALIZED


def test_embed_is_a_noop_once_already_embedded(classified_doc, vectors, fake_embedder):
    doc = classified_doc(["already in the vector db"], status=DocumentStatus.EMBEDDED)

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert vectors.upserts == 0


def test_a_fully_blank_document_embeds_zero_chunks_and_still_finishes(
    classified_doc, vectors, fake_embedder
):
    """A fax of a blank sheet is a finished document, not a failure. Raising here
    would burn MAX_ATTEMPTS to reach the same answer and then mislabel it FAILED."""
    doc = classified_doc(["", "   "])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    assert vectors.upserts == 0  # no empty upsert call at all
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.EMBEDDED
        assert _logs(s, doc)[0].detail["chunks"] == 0


def test_a_failed_upsert_leaves_the_document_at_classified(
    classified_doc, vectors, fake_embedder, monkeypatch
):
    """Vectors before status. If the vector DB is down, the document must stay
    retryable -- EMBEDDED with no vectors behind it is invisible to every RAG query
    and nothing would ever notice."""
    doc = classified_doc(["a page the vector db will refuse"])

    def boom(collection, points):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(vectors, "upsert", boom)

    with SessionLocal() as s:
        with pytest.raises(RuntimeError):
            handlers.handle_embed(s, s.get(Document, doc.id))
        s.rollback()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.CLASSIFIED
        assert _logs(s, doc) == []


def test_embed_enqueues_extract(tenant, classified_doc, vectors, fake_embedder):
    doc = classified_doc(["a page of text"])

    with SessionLocal() as s:
        handlers.handle_embed(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert [j.kind for j in _jobs(s, doc)] == [JobKind.EXTRACT]


# --- extract: EMBEDDED -> EXTRACTED or NEEDS_REVIEW ---------------------------


@pytest.fixture
def embedded_doc(tenant, make_doc, blob, monkeypatch):
    """An EMBEDDED claims document whose PDF holds `texts`, one string per page."""

    def _make(texts=("a page of claim text",), doc_type=DocumentType.CLAIMS):
        monkeypatch.setattr(pdftext, "pages", lambda pdf: enumerate(texts, start=1))
        doc = make_doc(status=DocumentStatus.EMBEDDED, page_count=len(texts))
        key = f"derived/{tenant}/{doc.id}/{doc_type.value}.pdf"
        blob.data[key] = FAKE_PDFA
        with SessionLocal() as s:
            fresh = s.get(Document, doc.id)
            fresh.normalized_blob_key = key
            fresh.doc_type = doc_type
            fresh.classify_confidence = 0.95
            s.commit()
        return doc

    return _make


def test_extract_missing_fields_populate_rows_and_send_to_review(
    embedded_doc, fake_extractor
):
    fake_extractor.result = extract.ExtractionResult(
        fields=[
            extract.ExtractedField(name="member_id", value="ABC-123", confidence=0.95)
        ],
        summary="A routine claim.",
    )
    doc = embedded_doc()

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    assert fake_extractor.seen == [(DocumentType.CLAIMS, "a page of claim text")]
    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.NEEDS_REVIEW
        assert fresh.summary == "A routine claim."
        rows = s.scalars(
            select(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc.id)
        ).all()
        by_name = {r.name: r for r in rows}
        assert by_name["member_id"].value == "ABC-123"
        assert by_name["member_id"].confidence == pytest.approx(0.95)
        # claim_number was expected but never returned: a row, not a gap, with
        # confidence 0.0 -- missing fields are what send this document to review.
        assert by_name["claim_number"].value is None
        assert by_name["claim_number"].confidence == 0.0
        [log] = _logs(s, doc)
        assert log.detail == {
            "to": "NEEDS_REVIEW",
            "fields": 4,
            "reasons": [
                "field_low_confidence:claim_number",
                "field_low_confidence:service_date",
                "field_low_confidence:billed_amount",
            ],
        }


def test_extract_every_expected_field_present_and_valid_lands_at_extracted(
    embedded_doc, fake_extractor
):
    fake_extractor.result = extract.ExtractionResult(
        fields=[
            extract.ExtractedField(name="member_id", value="ABC-123", confidence=0.95),
            extract.ExtractedField(name="claim_number", value="C1", confidence=0.95),
            extract.ExtractedField(
                name="service_date", value="2026-10-01", confidence=0.95
            ),
            extract.ExtractedField(
                name="billed_amount", value="120.00", confidence=0.95
            ),
        ],
        summary="A routine claim.",
    )
    doc = embedded_doc()

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.EXTRACTED
        [log] = _logs(s, doc)
        assert log.detail == {"to": "EXTRACTED", "fields": 4, "reasons": []}


def test_extract_an_invalid_field_sends_the_document_to_review(
    embedded_doc, fake_extractor
):
    fake_extractor.result = extract.ExtractionResult(
        fields=[
            extract.ExtractedField(name="member_id", value="ABC-123", confidence=0.95),
            extract.ExtractedField(name="claim_number", value="C1", confidence=0.95),
            extract.ExtractedField(
                name="service_date", value="not a real date", confidence=0.95
            ),
            extract.ExtractedField(
                name="billed_amount", value="120.00", confidence=0.95
            ),
        ],
        summary="A routine claim.",
    )
    doc = embedded_doc()

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW


def test_extract_unknown_doc_type_always_goes_to_review(embedded_doc, fake_extractor):
    doc = embedded_doc(doc_type=DocumentType.UNKNOWN)

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW
        rows = s.scalars(
            select(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc.id)
        ).all()
        assert rows == []  # UNKNOWN has no expected fields


def test_extract_a_type_with_no_expected_fields_passes_vacuously(
    embedded_doc, fake_extractor
):
    fake_extractor.result = extract.ExtractionResult(
        fields=[], summary="A letter about a billing dispute."
    )
    doc = embedded_doc(doc_type=DocumentType.CORRESPONDENCE)

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.EXTRACTED
        assert fresh.summary == "A letter about a billing dispute."


def test_a_refused_extraction_goes_to_review_even_with_no_expected_fields(
    embedded_doc, fake_extractor
):
    """With no expected fields, an empty field list would pass the gate -- a refusal
    must not ride that through to EXTRACTED with a blank summary."""
    fake_extractor.result = None
    doc = embedded_doc(doc_type=DocumentType.CORRESPONDENCE)

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.NEEDS_REVIEW
        assert fresh.summary is None
        assert _logs(s, doc)[0].detail["reasons"] == ["extract_refused"]


def test_an_invented_field_name_is_never_stored(embedded_doc, fake_extractor):
    fake_extractor.result = extract.ExtractionResult(
        fields=[extract.ExtractedField(name="made_up", value="x", confidence=0.99)],
        summary="s",
    )
    doc = embedded_doc(doc_type=DocumentType.CONSENT)

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        names = s.scalars(
            select(ExtractedFieldRow.name).where(ExtractedFieldRow.document_id == doc.id)
        ).all()
        assert sorted(names) == ["member_id", "signed_date"]


def test_the_database_refuses_a_second_row_for_the_same_field(tenant, make_doc):
    doc = make_doc()
    with SessionLocal() as s:
        for _ in range(2):
            s.add(
                ExtractedFieldRow(
                    tenant_id=tenant, document_id=doc.id, name="member_id",
                    value="A1", confidence=0.9,
                )
            )
        with pytest.raises(IntegrityError):
            s.commit()


def test_extract_sends_a_document_to_review_with_no_job(embedded_doc, fake_extractor):
    """A document in review waits for a human; resolve enqueues associate."""
    doc = embedded_doc()

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW
        assert _jobs(s, doc) == []


def test_extract_enqueues_associate_when_the_gate_passes(embedded_doc, fake_extractor):
    doc = embedded_doc(doc_type=DocumentType.CORRESPONDENCE)

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.EXTRACTED
        [job] = _jobs(s, doc)
        assert job.kind == JobKind.ASSOCIATE


def test_extract_is_a_noop_before_embed_ran(embedded_doc, fake_extractor):
    doc = embedded_doc()
    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        fresh.status = DocumentStatus.CLASSIFIED
        s.commit()

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    assert fake_extractor.seen == []
    with SessionLocal() as s:
        assert s.get(Document, doc.id).status == DocumentStatus.CLASSIFIED


def test_extract_is_a_noop_once_already_past_embedded(embedded_doc, fake_extractor):
    doc = embedded_doc()
    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        fresh.status = DocumentStatus.EXTRACTED
        s.commit()

    with SessionLocal() as s:
        handlers.handle_extract(s, s.get(Document, doc.id))
        s.commit()

    assert fake_extractor.seen == []
