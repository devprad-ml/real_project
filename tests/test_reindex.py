"""Rebuilding a tenant's collection from blob storage."""

import uuid

from app.enums import DocumentStatus
from app.jobs import reindex
from app.processing import pdftext
from app.storage.vectors import Point, tenant_collection


def test_reindex_rebuilds_only_embedded_documents_and_drops_stale_points(
    db, tenant, make_doc, blob, vectors, fake_embedder, monkeypatch
):
    monkeypatch.setattr(reindex, "get_vector_store", lambda: vectors)
    monkeypatch.setattr(pdftext, "pages", lambda pdf: iter([(1, "a page of text")]))
    key = "derived/x/claims.pdf"
    blob.data[key] = b"pdf"
    embedded = make_doc(DocumentStatus.EMBEDDED, normalized_blob_key=key)
    make_doc(DocumentStatus.CLASSIFIED, normalized_blob_key=key)
    # A point from the old schema, or from a document that no longer exists.
    collection = tenant_collection(tenant)
    vectors.upsert(collection, [Point(str(uuid.uuid4()), [1.0, 0.0, 0.0], {})])

    docs, chunks = reindex.reindex_tenant(db, tenant)

    assert (docs, chunks) == (1, 1)
    [point] = vectors.points(collection)
    assert point.payload["document_id"] == str(embedded.id)
