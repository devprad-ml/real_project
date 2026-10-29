''' AzureSearchVectorStore without a network: a recording client stands in for the SDK.

What these pin down is what the store itself decides: which filter it sends, which
tenant it writes, when it refuses. Whether the service agrees is the contract suite's
job (test_vector_contract.py, run against a real service when one is configured).
'''

import types
import uuid

import pytest
from azure.core.exceptions import ResourceNotFoundError

from app import storage
from app.storage.azure_search import BATCH, AzureSearchVectorStore, tenant_of
from app.storage.vectors import Point

TENANT = str(uuid.uuid4())
OTHER = str(uuid.uuid4())


class RecordingClient:
    def __init__(self):
        self.searches = []
        self.uploads = []
        self.deletes = []
        self.rows = []  # what the next search returns
        self.reject = set()  # keys the service refuses
        self.missing = False

    def search(self, **kw):
        self.searches.append(kw)
        if self.missing:
            raise ResourceNotFoundError("no such index")
        return list(self.rows)

    def merge_or_upload_documents(self, docs):
        self.uploads.append(docs)
        return [
            types.SimpleNamespace(key=d["id"], succeeded=d["id"] not in self.reject)
            for d in docs
        ]

    def delete_documents(self, docs):
        self.deletes.append(docs)


@pytest.fixture
def store():
    s = AzureSearchVectorStore("https://unused", "key", dim=3)
    s._client = RecordingClient()
    s._index_client = types.SimpleNamespace(
        create_or_update_index=lambda idx: None,
        get_service_statistics=lambda: None,
    )
    return s


def point(pid="p1", tenant_in_payload=OTHER, text="a chunk"):
    return Point(
        id=pid,
        vector=[1.0, 0.0, 0.0],
        payload={
            "tenant_id": tenant_in_payload, "document_id": str(uuid.uuid4()),
            "page_no": 1, "doc_type": "claims", "text": text,
        },
    )


def row(pid="p1"):
    return {
        "id": pid, "@search.score": 0.03, "tenant_id": TENANT, "document_id": "d",
        "page_no": 2, "doc_type": "denial", "content": "the text",
    }


def test_only_a_tenant_collection_name_is_accepted():
    assert tenant_of(f"chunks_{TENANT}") == TENANT
    for bad in ("chunks", "chunks_", "chunks_not-a-uuid", f"other_{TENANT}",
                f"chunks_{TENANT}' or tenant_id ne '"):
        with pytest.raises(ValueError):
            tenant_of(bad)


def test_a_bad_collection_name_fails_before_any_request(store):
    store._client = None  # touching the SDK client would try to connect and fail
    for op in (
        lambda: store.search("chunks_x", [0.0] * 3, ([], []), 5, "q"),
        lambda: store.upsert("chunks_x", [point()]),
        lambda: store.drop("chunks_x"),
    ):
        with pytest.raises(ValueError):
            op()


def test_every_search_carries_the_tenant_filter(store):
    store._client.rows = [row()]

    hits = store.search(f"chunks_{TENANT}", [1.0, 0.0, 0.0], ([], []), 5, "claim C-77")

    [sent] = store._client.searches
    assert sent["filter"] == f"tenant_id eq '{TENANT}'"
    assert sent["search_text"] == "claim C-77"
    assert sent["top"] == 5
    [vq] = sent["vector_queries"]
    assert vq.fields == "vector" and vq.k_nearest_neighbors == 20
    assert hits[0].payload == {
        "tenant_id": TENANT, "document_id": "d", "page_no": 2,
        "doc_type": "denial", "text": "the text",
    }
    assert hits[0].id == "p1" and hits[0].score == 0.03


def test_an_empty_query_text_is_a_vector_only_search(store):
    store.search(f"chunks_{TENANT}", [1.0, 0.0, 0.0], ([], []), 5, "")
    assert store._client.searches[0]["search_text"] is None


def test_a_point_is_written_into_the_tenant_it_was_addressed_to(store):
    """The payload names another tenant; the collection name wins."""
    store.upsert(f"chunks_{TENANT}", [point(tenant_in_payload=OTHER)])

    [[doc]] = store._client.uploads
    assert doc["tenant_id"] == TENANT
    assert doc["content"] == "a chunk" and doc["vector"] == [1.0, 0.0, 0.0]


def test_upsert_goes_out_in_batches(store):
    store.upsert(f"chunks_{TENANT}", [point(f"p{i}") for i in range(BATCH * 2 + 1)])
    assert [len(b) for b in store._client.uploads] == [BATCH, BATCH, 1]


def test_a_rejected_chunk_fails_the_upsert(store):
    """A batch that 'succeeds' with a document missing must not read as success: the
    embed job would flip the document to EMBEDDED with chunks absent."""
    store._client.reject = {"p2"}
    with pytest.raises(RuntimeError, match="1 of 3"):
        store.upsert(f"chunks_{TENANT}", [point("p1"), point("p2"), point("p3")])


def test_search_before_the_first_write_is_empty_not_an_error(store):
    store._client.missing = True
    assert store.search(f"chunks_{TENANT}", [1.0, 0.0, 0.0], ([], []), 5, "q") == []


def test_drop_pages_through_the_tenant_and_stops(store):
    pages = iter([[{"id": "a"}, {"id": "b"}], [{"id": "c"}], []])
    store._client.search = lambda **kw: (
        store._client.searches.append(kw) or next(pages)
    )

    store.drop(f"chunks_{TENANT}")

    assert store._client.deletes == [[{"id": "a"}, {"id": "b"}], [{"id": "c"}]]
    assert all(s["filter"] == f"tenant_id eq '{TENANT}'" for s in store._client.searches)


def test_drop_stops_when_the_index_still_lists_what_it_just_deleted(store):
    """Eventual consistency: the same ids come back once. Not a reason to loop."""
    store._client.search = lambda **kw: [{"id": "a"}]
    store.drop(f"chunks_{TENANT}")
    assert store._client.deletes == [[{"id": "a"}]]


def test_drop_with_no_index_is_a_noop(store):
    store._client.missing = True
    store.drop(f"chunks_{TENANT}")
    assert store._client.deletes == []


# --- choosing the backend --------------------------------------------------------


@pytest.fixture
def settings(monkeypatch):
    def use(**kw):
        base = dict(
            vector_backend="qdrant", qdrant_url="http://unused", embed_dim=3,
            azure_search_endpoint="", azure_search_api_key=None,
            azure_search_index="chunks",
        )
        monkeypatch.setattr(storage, "get_settings", lambda: types.SimpleNamespace(
            **{**base, **kw}))
        storage.get_vector_store.cache_clear()

    yield use
    storage.get_vector_store.cache_clear()


def test_the_backend_setting_selects_the_class(settings):
    from pydantic import SecretStr

    from app.storage.vectors import QdrantVectorStore

    settings()
    assert isinstance(storage.get_vector_store(), QdrantVectorStore)

    settings(vector_backend="azure", azure_search_endpoint="https://x",
             azure_search_api_key=SecretStr("k"))
    assert isinstance(storage.get_vector_store(), AzureSearchVectorStore)


def test_azure_without_credentials_and_unknown_backends_refuse_to_start(settings):
    settings(vector_backend="azure")
    with pytest.raises(ValueError, match="endpoint"):
        storage.get_vector_store()
    settings(vector_backend="pinecone")
    with pytest.raises(ValueError, match="unknown vector_backend"):
        storage.get_vector_store()
