"""The review queue (/validation), the document file, and the demo page. Real DB,
fake blob store."""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import AuditAction, DocumentStatus, DocumentType, JobKind, Role
from app.models import Document, ExtractedFieldRow, Job, Log
from app.storage.blob import normalized_key

PDF = b"%PDF-1.7 the normalized document"


@pytest.fixture
def in_review(make_doc, blob):
    """Factory: a NEEDS_REVIEW document as handle_extract leaves it -- normalized PDF
    in the blob store, one row per field, and the audit row with its reasons."""

    def _make(doc_type=DocumentType.CLAIMS, fields=None, reasons=("r",), **kwargs):
        doc = make_doc(DocumentStatus.NEEDS_REVIEW, **kwargs)
        with SessionLocal() as s:
            doc = s.get(Document, doc.id)
            doc.doc_type = doc_type
            doc.classify_confidence = 0.6
            doc.normalized_blob_key = normalized_key(doc.tenant_id, doc.id, doc_type)
            blob.put(doc.normalized_blob_key, PDF, "application/pdf")
            for name, (value, conf) in (fields or {}).items():
                s.add(
                    ExtractedFieldRow(
                        tenant_id=doc.tenant_id,
                        document_id=doc.id,
                        name=name,
                        value=value,
                        confidence=conf,
                    )
                )
            s.add(
                Log(
                    tenant_id=doc.tenant_id,
                    document_id=doc.id,
                    actor="system",
                    action=AuditAction.STATE_CHANGE,
                    detail={"to": "NEEDS_REVIEW", "reasons": list(reasons)},
                )
            )
            s.commit()
            return doc

    return _make


@pytest.fixture
def reviewer(staff):
    return staff(Role.REVIEWER)


def fresh(doc_id):
    with SessionLocal() as s:
        doc = s.get(Document, doc_id)
        fields = {
            f.name: f
            for f in s.scalars(
                select(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc_id)
            )
        }
        audit = s.scalars(
            select(Log).where(Log.document_id == doc_id).order_by(Log.at.desc())
        ).first()
        return doc, fields, audit


CLAIM_FIELDS = {
    "member_id": ("ABC-123", 0.95),
    "claim_number": (None, 0.0),
    "service_date": ("2026-10-01", 0.9),
    "billed_amount": ("120.00", 0.9),
}


# --- GET /validation -----------------------------------------------------------


def test_the_queue_shows_fields_in_schema_order_with_reasons(
    client, reviewer, in_review
):
    doc = in_review(
        fields=CLAIM_FIELDS,
        reasons=["field_low_confidence:claim_number"],
        source_filename="claim.pdf",
    )

    [item] = client.get("/validation", headers=reviewer).json()

    assert item["id"] == str(doc.id)
    assert item["filename"] == "claim.pdf"
    assert item["reasons"] == ["field_low_confidence:claim_number"]
    assert [f["name"] for f in item["fields"]] == list(CLAIM_FIELDS)
    assert item["fields"][1] == {
        "name": "claim_number",
        "value": None,
        "confidence": 0.0,
        "human_verified": False,
    }


def test_only_documents_in_review_oldest_first(client, reviewer, in_review, make_doc):
    first = in_review()
    second = in_review()
    make_doc(DocumentStatus.EXTRACTED)

    ids = [i["id"] for i in client.get("/validation", headers=reviewer).json()]

    assert ids == [str(first.id), str(second.id)]


def test_another_tenants_queue_is_invisible(client, staff, in_review, other_tenant):
    in_review()
    headers = staff(Role.REVIEWER, tenant_id=other_tenant)
    assert client.get("/validation", headers=headers).json() == []


# --- POST /validation/{id}/resolve -------------------------------------------------


def test_resolve_corrects_confirms_and_clears_the_document(client, reviewer, in_review):
    doc = in_review(fields=CLAIM_FIELDS)

    r = client.post(
        f"/validation/{doc.id}/resolve",
        json={"fields": {"claim_number": "C-77"}},
        headers=reviewer,
    )

    assert r.status_code == 204
    after, fields, audit = fresh(doc.id)
    assert after.status == DocumentStatus.EXTRACTED
    assert fields["claim_number"].value == "C-77"
    # untouched fields are confirmed too: the reviewer saw the whole list
    assert all(f.human_verified and f.confidence == 1.0 for f in fields.values())
    assert fields["member_id"].value == "ABC-123"
    assert audit.action == AuditAction.RESOLVE
    assert audit.actor.startswith("user:")
    assert audit.detail == {
        "to": "EXTRACTED",
        "changed": {"claim_number": [None, "C-77"]},
    }


def _jobs(doc_id):
    with SessionLocal() as s:
        return [j.kind for j in s.scalars(select(Job).where(Job.document_id == doc_id))]


def test_resolve_sends_the_document_on_to_associate(client, reviewer, in_review):
    doc = in_review(fields=CLAIM_FIELDS)
    client.post(f"/validation/{doc.id}/resolve", json={}, headers=reviewer)
    assert _jobs(doc.id) == [JobKind.ASSOCIATE]


def test_no_mpi_match_skips_matching_and_routes_unbound(client, reviewer, in_review):
    """A new enrollee not in the MPI yet: matching would only send it back here."""
    doc = in_review(fields=CLAIM_FIELDS, reasons=["member_not_found"])

    r = client.post(
        f"/validation/{doc.id}/resolve", json={"no_mpi_match": True}, headers=reviewer
    )

    assert r.status_code == 204
    after, _, audit = fresh(doc.id)
    assert after.status == DocumentStatus.ASSOCIATED
    assert audit.detail == {"to": "ASSOCIATED", "changed": {}, "no_mpi_match": True}
    assert _jobs(doc.id) == [JobKind.ROUTE]


def test_a_second_resolve_is_a_409(client, reviewer, in_review):
    doc = in_review(fields=CLAIM_FIELDS)
    url = f"/validation/{doc.id}/resolve"
    assert client.post(url, json={}, headers=reviewer).status_code == 204
    assert client.post(url, json={}, headers=reviewer).status_code == 409


def test_a_field_outside_the_schema_is_a_422(client, reviewer, in_review):
    doc = in_review(fields=CLAIM_FIELDS)
    r = client.post(
        f"/validation/{doc.id}/resolve",
        json={"fields": {"denial_code": "X1"}},
        headers=reviewer,
    )
    assert r.status_code == 422
    assert fresh(doc.id)[0].status == DocumentStatus.NEEDS_REVIEW


def test_a_value_that_fails_its_validator_is_a_422(client, reviewer, in_review):
    doc = in_review(fields=CLAIM_FIELDS)
    r = client.post(
        f"/validation/{doc.id}/resolve",
        json={"fields": {"service_date": "last tuesday"}},
        headers=reviewer,
    )
    assert r.status_code == 422
    assert "service_date" in r.json()["detail"]


def test_an_unknown_type_must_be_reclassified_first(client, reviewer, in_review):
    doc = in_review(doc_type=DocumentType.UNKNOWN)
    r = client.post(f"/validation/{doc.id}/resolve", json={}, headers=reviewer)
    assert r.status_code == 409


@pytest.mark.parametrize(
    "path, body",
    [
        ("/validation/{id}/resolve", {}),
        ("/validation/{id}/reclassify", {"doc_type": "denial"}),
    ],
)
def test_another_tenants_document_is_a_404(
    client, staff, in_review, other_tenant, path, body
):
    """404, not 403: a 403 would confirm the id exists."""
    doc = in_review()
    headers = staff(Role.REVIEWER, tenant_id=other_tenant)

    r = client.post(path.format(id=doc.id), json=body, headers=headers)

    assert r.status_code == 404
    assert fresh(doc.id)[0].status == DocumentStatus.NEEDS_REVIEW


# --- POST /validation/{id}/reclassify ----------------------------------------------


def test_reclassify_moves_the_pdf_and_sends_it_back_through_embed(
    client, reviewer, in_review, blob
):
    doc = in_review(doc_type=DocumentType.CLAIMS, fields=CLAIM_FIELDS)
    old_key = doc.normalized_blob_key

    r = client.post(
        f"/validation/{doc.id}/reclassify", json={"doc_type": "denial"}, headers=reviewer
    )

    assert r.status_code == 202
    after, fields, audit = fresh(doc.id)
    assert after.doc_type == DocumentType.DENIAL
    assert after.status == DocumentStatus.CLASSIFIED
    # a human chose it: the gate must not send it straight back for low confidence
    assert after.classify_confidence == 1.0
    assert after.normalized_blob_key.endswith("/denial.pdf")
    assert blob.data[after.normalized_blob_key] == PDF
    assert old_key not in blob.data  # deleted after commit
    assert fields == {}  # the old schema's fields are gone...
    assert audit.action == AuditAction.RECLASSIFY
    assert audit.detail["from"] == "claims" and audit.detail["to"] == "denial"
    assert audit.detail["fields"]["member_id"] == "ABC-123"  # ...but not forgotten
    with SessionLocal() as s:
        [job] = s.scalars(select(Job).where(Job.document_id == doc.id)).all()
        assert job.kind == JobKind.EMBED


def test_reclassify_to_unknown_is_a_422(client, reviewer, in_review):
    doc = in_review()
    r = client.post(
        f"/validation/{doc.id}/reclassify", json={"doc_type": "unknown"}, headers=reviewer
    )
    assert r.status_code == 422


def test_reclassify_outside_review_is_a_409(client, reviewer, make_doc):
    doc = make_doc(DocumentStatus.EXTRACTED)
    r = client.post(
        f"/validation/{doc.id}/reclassify", json={"doc_type": "denial"}, headers=reviewer
    )
    assert r.status_code == 409


# --- GET /documents/{id}/file, GET /review -------------------------------------------


def test_the_file_is_served_and_the_view_audited(client, staff, in_review):
    doc = in_review()

    r = client.get(f"/documents/{doc.id}/file", headers=staff())

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == PDF
    with SessionLocal() as s:
        view = s.scalar(
            select(Log).where(Log.document_id == doc.id, Log.action == AuditAction.VIEW)
        )
    assert view.detail == {"via": "file"}


def test_the_file_needs_a_key(client, in_review):
    doc = in_review()
    assert client.get(f"/documents/{doc.id}/file").status_code == 401


def test_another_tenants_file_is_a_404(client, staff, in_review, other_tenant):
    doc = in_review()
    headers = staff(tenant_id=other_tenant)
    assert client.get(f"/documents/{doc.id}/file", headers=headers).status_code == 404


def test_the_review_page_is_served(client):
    r = client.get("/review")
    assert r.status_code == 200
    assert "Review queue" in r.text
