''' The read path: retrieve.search and GET /search. Real DB, fake vector store,
encode pinned to a fixed query vector so the ranking is decided by the test. '''

import uuid

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import AuditAction, DocumentStatus, DocumentType
from app.models.audit_log import Log
from app.processing import bm25, embed
from app.search.retrieve import search
from app.storage.vectors import Point, point_id, tenant_collection

QUERY_VECTOR = [1.0, 0.0, 0.0]


@pytest.fixture
def query_encoder(monkeypatch):
    monkeypatch.setattr(embed, "encode", lambda texts: [QUERY_VECTOR for _ in texts])


@pytest.fixture
def plant(vectors, tenant):
    ''' Put a chunk straight into the tenant's collection, as handle_embed would. '''

    def _plant(document_id, vector, *, page_no=1, idx=0, text="chunk"):
        vectors.upsert(
            tenant_collection(tenant),
            [
                Point(
                    id=point_id(document_id, page_no, idx),
                    vector=vector,
                    sparse=bm25.encode_doc(text),
                    payload={
                        "tenant_id": str(tenant),
                        "document_id": str(document_id),
                        "page_no": page_no,
                        "text": text,
                    },
                )
            ],
        )

    return _plant


def views(tenant):
    with SessionLocal() as s:
        return s.scalars(
            select(Log).where(Log.tenant_id == tenant, Log.action == AuditAction.VIEW)
        ).all()


# --- retrieve.search ---------------------------------------------------------


def test_hits_come_back_best_first_with_their_citation(
    db, tenant, make_doc, plant, query_encoder
):
    near = make_doc(DocumentStatus.EMBEDDED, source_filename="denial.pdf")
    far = make_doc(DocumentStatus.CLASSIFIED, source_filename="claim.pdf")
    plant(far.id, [0.0, 1.0, 0.0], page_no=1, text="unrelated")
    plant(near.id, [0.9, 0.1, 0.0], page_no=3, text="the denial reason")

    hits = search(db, tenant_id=tenant, query="why was it denied")

    assert [h.document_id for h in hits] == [near.id, far.id]
    top = hits[0]
    assert (top.filename, top.page_no, top.text) == ("denial.pdf", 3, "the denial reason")
    assert top.status == DocumentStatus.EMBEDDED
    # The type comes from Postgres, not the payload: make_doc files everything UNKNOWN.
    assert top.doc_type == DocumentType.UNKNOWN
    # status is returned, not filtered: a half-embedded document is still true text
    assert hits[1].status == DocumentStatus.CLASSIFIED
    assert hits[0].score > hits[1].score


def test_an_exact_token_match_outranks_a_closer_vector(
    db, tenant, make_doc, plant, query_encoder
):
    """The reason for the keyword leg. A member ID means nothing to the dense model;
    the chunk that literally contains it must still come first."""
    by_meaning = make_doc(DocumentStatus.EMBEDDED)
    by_token = make_doc(DocumentStatus.EMBEDDED)
    plant(by_meaning.id, [1.0, 0.0, 0.0], text="a member's coverage was reviewed")
    plant(by_token.id, [0.6, 0.4, 0.0], text="member MBR-0048213 appeal received")

    hits = search(db, tenant_id=tenant, query="MBR-0048213")

    assert [h.document_id for h in hits] == [by_token.id, by_meaning.id]


def test_a_tenant_with_nothing_embedded_gets_nothing(db, tenant, vectors, query_encoder):
    assert search(db, tenant_id=tenant, query="anything") == []
    db.commit()
    assert views(tenant) == []


def test_a_hit_for_a_document_this_tenant_does_not_own_is_dropped(
    db, tenant, make_doc, plant, query_encoder
):
    mine = make_doc(DocumentStatus.EMBEDDED)
    # No row for this tenant: a point in the wrong collection, or a deleted document
    # whose vectors linger. Either way it must not be served.
    plant(uuid.uuid4(), [1.0, 0.0, 0.0], text="someone else's chart")
    plant(mine.id, [0.5, 0.5, 0.0])

    hits = search(db, tenant_id=tenant, query="q")

    assert [h.document_id for h in hits] == [mine.id]


def test_one_view_row_per_document_not_per_chunk(
    db, tenant, make_doc, plant, query_encoder
):
    doc = make_doc(DocumentStatus.EMBEDDED)
    plant(doc.id, [1.0, 0.0, 0.0], page_no=1, idx=0)
    plant(doc.id, [0.8, 0.2, 0.0], page_no=2, idx=0)

    hits = search(db, tenant_id=tenant, query="the query")
    db.commit()

    assert len(hits) == 2
    rows = views(tenant)
    assert len(rows) == 1
    assert rows[0].document_id == doc.id
    assert rows[0].detail == {"via": "search", "query": "the query"}


# --- GET /search ---------------------------------------------------------------


def test_the_endpoint_returns_hits_and_commits_the_audit(
    client, tenant, staff, make_doc, plant, query_encoder
):
    doc = make_doc(DocumentStatus.EMBEDDED, source_filename="appeal.pdf")
    plant(doc.id, [1.0, 0.0, 0.0], page_no=2, text="appeal granted")

    r = client.get("/search", params={"q": "appeal"}, headers=staff())

    assert r.status_code == 200
    [hit] = r.json()
    assert hit["document_id"] == str(doc.id)
    assert (hit["filename"], hit["page_no"], hit["text"]) == (
        "appeal.pdf",
        2,
        "appeal granted",
    )
    # read from a fresh session: proves the endpoint committed, not just staged
    [view] = views(tenant)
    assert view.document_id == doc.id
    assert view.actor.startswith("user:")


def test_the_tenant_comes_from_the_key_not_a_header(
    client, tenant, staff, make_doc, plant, query_encoder
):
    """The old trusted X-Tenant-Id header is ignored: a key only ever searches its
    own tenant's collection."""
    doc = make_doc(DocumentStatus.EMBEDDED)
    plant(doc.id, [1.0, 0.0, 0.0])
    headers = {**staff(), "X-Tenant-Id": str(uuid.uuid4())}

    r = client.get("/search", params={"q": "x"}, headers=headers)

    assert [h["document_id"] for h in r.json()] == [str(doc.id)]


def test_no_key_is_a_401(client, vectors, query_encoder):
    assert client.get("/search", params={"q": "x"}).status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"q": ""},  # empty query
        {"q": "x", "limit": 51},  # over the cap
    ],
)
def test_bad_input_is_a_422(client, staff, vectors, query_encoder, params):
    assert client.get("/search", params=params, headers=staff()).status_code == 422
