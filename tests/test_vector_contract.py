''' One behaviour suite, run against every VectorStore.

Fake: always. Qdrant: when one answers. Azure AI Search: only when AZURE_SEARCH_ENDPOINT
and AZURE_SEARCH_API_KEY are set, against a throwaway index that is deleted afterwards
(the free tier is enough). This is what keeps the fake honest: a fake written from
memory agrees with the code that was written from the same memory, and nothing else.

Hand-made three-dimensional vectors, no torch: these test the store, not the model.
'''

import os
import time
import uuid

import pytest

from app.config import get_settings
from app.processing import bm25
from app.storage.vectors import Point, QdrantVectorStore

DOWN = [1.0, 0.0, 0.0]
EVENTUALLY_SECONDS = 30  # Azure makes a write searchable a little after it returns


def _azure():
    from app.storage.azure_search import AzureSearchVectorStore

    store = AzureSearchVectorStore(
        os.environ["AZURE_SEARCH_ENDPOINT"], os.environ["AZURE_SEARCH_API_KEY"],
        dim=3, index=f"test{uuid.uuid4().hex[:12]}",
    )
    return store


@pytest.fixture(params=["fake", "qdrant", "azure"])
def store(request):
    if request.param == "fake":
        yield request.getfixturevalue("vectors")
    elif request.param == "qdrant":
        s = QdrantVectorStore(get_settings().qdrant_url, dim=3)
        try:
            s.client.get_collections()
        except Exception:
            pytest.skip(f"qdrant not reachable at {s.url}")
        s.created = []
        yield s
        for name in s.created:
            s.drop(name)
    else:
        if not (os.environ.get("AZURE_SEARCH_ENDPOINT")
                and os.environ.get("AZURE_SEARCH_API_KEY")):
            pytest.skip("AZURE_SEARCH_ENDPOINT / AZURE_SEARCH_API_KEY not set")
        s = _azure()
        yield s
        s.index_client.delete_index(s.index)


def tenant(store) -> str:
    ''' A new tenant's collection name, in the shape every store accepts. '''
    name = f"chunks_{uuid.uuid4()}"
    getattr(store, "created", []).append(name)
    return name


def chunk(collection, text, vector=DOWN, pid=None):
    return Point(
        id=pid or str(uuid.uuid4()),
        vector=vector,
        payload={
            "tenant_id": collection.removeprefix("chunks_"),
            "document_id": str(uuid.uuid4()),
            "page_no": 3,
            "doc_type": "denial",
            "text": text,
        },
        sparse=bm25.encode_doc(text),
    )


def find(store, collection, text="", limit=10):
    return store.search(collection, DOWN, bm25.encode_query(text), limit, text)


def wait_for(store, collection, count, text=""):
    ''' Hits once the write is visible. Immediate for fake and qdrant. '''
    deadline = time.monotonic() + EVENTUALLY_SECONDS
    while True:
        hits = find(store, collection, text)
        if len(hits) >= count or time.monotonic() > deadline:
            return hits
        time.sleep(1)


def test_a_chunk_comes_back_with_its_payload(store):
    c = tenant(store)
    p = chunk(c, "denied, reason code CO-50")
    store.upsert(c, [p])

    [hit] = wait_for(store, c, 1)

    assert hit.id == p.id
    for key in ("tenant_id", "document_id", "page_no", "doc_type", "text"):
        assert hit.payload[key] == p.payload[key]


def test_writing_the_same_id_twice_leaves_one_chunk(store):
    c = tenant(store)
    pid = str(uuid.uuid4())
    store.upsert(c, [chunk(c, "first version", pid=pid)])
    store.upsert(c, [chunk(c, "second version", pid=pid)])

    wait_for(store, c, 1)
    if hasattr(store, "index_client"):
        time.sleep(5)  # Azure: give a stray second copy time to become visible
    hits = find(store, c)

    assert [h.payload["text"] for h in hits] == ["second version"]


def test_an_exact_token_outranks_chunks_that_are_equally_near(store):
    """All three chunks are the same distance from the query vector; only the keyword
    leg can tell them apart."""
    c = tenant(store)
    store.upsert(c, [
        chunk(c, "invoice llama4412 paid in full"),
        chunk(c, "invoice zebra7731 paid in full"),
        chunk(c, "invoice walrus9090 paid in full"),
    ])
    wait_for(store, c, 3)

    hits = find(store, c, "zebra7731")

    assert "zebra7731" in hits[0].payload["text"]


def test_a_tenant_with_no_chunks_gets_nothing_not_an_error(store):
    assert find(store, tenant(store), "anything") == []


def test_one_tenant_never_sees_anothers_chunks(store):
    """The same text and vector in two tenants. Each search returns its own chunk
    only. On Azure this is a filter on one shared index; on qdrant it is two
    collections. The test does not care which."""
    a, b = tenant(store), tenant(store)
    pa, pb = chunk(a, "shared words here"), chunk(b, "shared words here")
    store.upsert(a, [pa])
    store.upsert(b, [pb])
    wait_for(store, a, 1)
    wait_for(store, b, 1)

    assert [h.id for h in find(store, a, "shared words")] == [pa.id]
    assert [h.id for h in find(store, b, "shared words")] == [pb.id]


def test_drop_removes_one_tenant_and_leaves_the_other(store):
    a, b = tenant(store), tenant(store)
    store.upsert(a, [chunk(a, "alpha text"), chunk(a, "more alpha")])
    store.upsert(b, [chunk(b, "beta text")])
    wait_for(store, a, 2)
    wait_for(store, b, 1)

    store.drop(a)
    deadline = time.monotonic() + EVENTUALLY_SECONDS
    while find(store, a) and time.monotonic() < deadline:
        time.sleep(1)

    assert find(store, a) == []
    assert len(find(store, b)) == 1
    store.drop(a)  # dropping again is a no-op


def test_ping_answers_on_a_reachable_store(store):
    store.ping()
