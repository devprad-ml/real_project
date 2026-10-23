"""M9: MPI matching, routing, the associate/route handlers, and the CSV loader."""

import datetime

import pytest
from sqlalchemy import select

from app import members
from app.db import SessionLocal
from app.enums import DocumentStatus, DocumentType, JobKind, RouteQueue
from app.errors import PermanentError
from app.jobs import handlers
from app.models import Association, Document, ExtractedFieldRow, Job, Log, Member
from app.processing import mpi, routing

DOB = datetime.date(1980, 5, 17)


# --- pure ------------------------------------------------------------------------


def test_normalize_id_ignores_case_and_punctuation():
    assert mpi.normalize_id(" abc-12 3 ") == "ABC123"


@pytest.mark.parametrize(
    ("member_id", "dob", "member", "reasons"),
    [
        (None, None, None, ["member_id_missing"]),
        ("ABC123", None, None, ["member_not_found"]),
        ("ABC123", "1980-05-18", Member(dob=DOB), ["dob_mismatch"]),
        ("ABC123", "05/17/1980", Member(dob=DOB), []),  # same day, other format
        ("ABC123", None, Member(dob=DOB), []),  # no dob on the doc: nothing to contradict
        ("ABC123", "1980-05-18", Member(dob=None), []),  # MPI has none
    ],
)
def test_mismatches(member_id, dob, member, reasons):
    assert mpi.mismatches(member_id, dob, member) == reasons


def test_route_by_doc_type():
    assert routing.route(DocumentType.APPEAL, True) == RouteQueue.APPEALS
    assert routing.route(DocumentType.PRIOR_AUTH, True) == RouteQueue.UTILIZATION_MGMT


def test_an_unbound_claim_goes_to_member_services_not_claims_ops():
    assert routing.route(DocumentType.CLAIMS, False) == RouteQueue.MEMBER_SERVICES


def test_correspondence_needs_no_member():
    assert routing.route(DocumentType.CORRESPONDENCE, False) == RouteQueue.MEMBER_SERVICES


def test_a_type_with_no_rule_fails_loudly():
    with pytest.raises(PermanentError):
        routing.route(DocumentType.UNKNOWN, True)


def test_every_known_type_has_a_rule():
    assert set(routing.RULES) == set(DocumentType) - {DocumentType.UNKNOWN}


# --- handlers -------------------------------------------------------------------


@pytest.fixture
def member(tenant):
    with SessionLocal() as s:
        m = Member(tenant_id=tenant, member_id="ABC123", dob=DOB)
        s.add(m)
        s.commit()
        return m


@pytest.fixture
def extracted_doc(tenant, make_doc):
    """Factory: a document at EXTRACTED (or `status`) with these field values."""

    def _make(doc_type=DocumentType.CLAIMS, status=DocumentStatus.EXTRACTED, **fields):
        doc = make_doc(status)
        with SessionLocal() as s:
            s.get(Document, doc.id).doc_type = doc_type
            for name, value in fields.items():
                s.add(
                    ExtractedFieldRow(
                        tenant_id=tenant,
                        document_id=doc.id,
                        name=name,
                        value=value,
                        confidence=0.9,
                    )
                )
            s.commit()
        return doc

    return _make


def run(handler, doc):
    with SessionLocal() as s:
        handler(s, s.get(Document, doc.id))
        s.commit()


def state(doc):
    with SessionLocal() as s:
        d = s.get(Document, doc.id)
        assoc = s.scalar(select(Association).where(Association.document_id == doc.id))
        jobs = [j.kind for j in s.scalars(select(Job).where(Job.document_id == doc.id))]
        log = s.scalars(
            select(Log).where(Log.document_id == doc.id).order_by(Log.at.desc())
        ).first()
        return d, assoc, jobs, log


def test_an_exact_match_binds_and_enqueues_route(extracted_doc, member):
    doc = extracted_doc(member_id="abc-123")

    run(handlers.handle_associate, doc)

    d, assoc, jobs, log = state(doc)
    assert d.status == DocumentStatus.ASSOCIATED
    assert assoc.member_pk == member.id
    assert assoc.match_confidence == pytest.approx(0.9)
    assert jobs == [JobKind.ROUTE]
    assert log.detail == {"to": "ASSOCIATED", "member": str(member.id)}


def test_no_member_goes_to_review_with_the_reason(extracted_doc, member):
    doc = extracted_doc(member_id="ZZZ999")

    run(handlers.handle_associate, doc)

    d, assoc, jobs, log = state(doc)
    assert d.status == DocumentStatus.NEEDS_REVIEW
    assert assoc is None
    assert jobs == []
    # the shape GET /validation reads its reasons from
    assert log.detail == {"to": "NEEDS_REVIEW", "reasons": ["member_not_found"]}


def test_a_dob_that_contradicts_the_mpi_goes_to_review(extracted_doc, member):
    doc = extracted_doc(
        DocumentType.MEDICAL_RECORD, member_id="ABC123", dob="1999-01-01"
    )

    run(handlers.handle_associate, doc)

    d, _, _, log = state(doc)
    assert d.status == DocumentStatus.NEEDS_REVIEW
    assert log.detail["reasons"] == ["dob_mismatch"]


def test_another_tenants_member_is_not_a_match(extracted_doc, other_tenant):
    with SessionLocal() as s:
        s.add(Member(tenant_id=other_tenant, member_id="ABC123"))
        s.commit()
    doc = extracted_doc(member_id="ABC123")

    run(handlers.handle_associate, doc)

    assert state(doc)[0].status == DocumentStatus.NEEDS_REVIEW


def test_correspondence_associates_with_nobody(extracted_doc):
    doc = extracted_doc(DocumentType.CORRESPONDENCE)

    run(handlers.handle_associate, doc)

    d, assoc, jobs, log = state(doc)
    assert d.status == DocumentStatus.ASSOCIATED
    assert assoc is None
    assert jobs == [JobKind.ROUTE]
    assert log.detail == {"to": "ASSOCIATED", "member": None}


def test_associate_is_a_noop_unless_extracted(extracted_doc, member):
    doc = extracted_doc(status=DocumentStatus.ASSOCIATED, member_id="ABC123")

    run(handlers.handle_associate, doc)

    _, assoc, jobs, log = state(doc)
    assert (assoc, jobs, log) == (None, [], None)


def test_route_sets_the_queue_and_ends_the_chain(extracted_doc, member):
    doc = extracted_doc(DocumentType.APPEAL, member_id="ABC123")
    run(handlers.handle_associate, doc)

    run(handlers.handle_route, doc)

    d, _, jobs, log = state(doc)
    assert d.status == DocumentStatus.ROUTED
    assert d.route_queue == RouteQueue.APPEALS
    assert jobs == [JobKind.ROUTE]  # the one associate enqueued; route adds none
    assert log.detail == {"to": "ROUTED", "queue": "appeals"}


def test_an_unbound_claim_routes_to_member_services(extracted_doc):
    doc = extracted_doc(status=DocumentStatus.ASSOCIATED, member_id="ABC123")

    run(handlers.handle_route, doc)

    assert state(doc)[0].route_queue == RouteQueue.MEMBER_SERVICES


def test_route_is_a_noop_unless_associated(extracted_doc):
    doc = extracted_doc()

    run(handlers.handle_route, doc)

    d, _, _, log = state(doc)
    assert (d.status, d.route_queue, log) == (DocumentStatus.EXTRACTED, None, None)


# --- CSV loader -----------------------------------------------------------------


def test_load_normalizes_and_upserts(tenant, tmp_path):
    csv = tmp_path / "members.csv"
    csv.write_text(
        "member_id,first_name,last_name,dob\n"
        "abc-123,Ann,Lee,1980-05-17\n"
        "XYZ9,Bo,,\n"
    )
    assert members.load(tenant, csv) == 2

    csv.write_text("member_id,first_name,last_name,dob\nABC123,Anne,Lee,1980-05-17\n")
    assert members.load(tenant, csv) == 1

    with SessionLocal() as s:
        rows = {
            m.member_id: m
            for m in s.scalars(select(Member).where(Member.tenant_id == tenant))
        }
    assert set(rows) == {"ABC123", "XYZ9"}
    assert rows["ABC123"].first_name == "Anne"  # updated, not duplicated
    assert rows["XYZ9"].dob is None


def test_load_rejects_a_bad_row_with_its_line_number(tenant, tmp_path):
    csv = tmp_path / "members.csv"
    csv.write_text("member_id,first_name,last_name,dob\nA1,,,17/05/1980\n")
    with pytest.raises(SystemExit, match="members.csv:2: dob"):
        members.load(tenant, csv)
