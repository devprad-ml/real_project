''' QdrantVectorStore against a real qdrant. Skipped when none answers.

Every other test uses FakeVectorStore. This file is the check that the real store
agrees with the fake on the behaviours the pipeline depends on. Hand-written
three-dimensional vectors, no torch: this tests the store, not the model.
'''

import uuid

import pytest

from app.config import get_settings
from app.processing import bm25
from app.storage.vectors import Point, QdrantVectorStore

NO_KEYWORDS = ([], [])


@pytest.fixture
def store():
    s = QdrantVectorStore(get_settings().qdrant_url, dim=3)
    try:
        s.client.get_collections()
    except Exception:
        pytest.skip(f"qdrant not reachable at {s.url}")
    name = f"test_{uuid.uuid4().hex}"
    yield s, name
    if s.client.collection_exists(name):
        s.drop(name)


def pt(vector, text):
    return Point(
        id=str(uuid.uuid4()),
        vector=vector,
        payload={"text": text},
        sparse=bm25.encode_doc(text),
    )


def test_the_nearest_point_comes_back_first_with_its_payload(store):
    s, name = store
    s.upsert(name, [pt([0.0, 1.0, 0.0], "far"), pt([1.0, 0.1, 0.0], "near")])

    hits = s.search(name, [1.0, 0.0, 0.0], NO_KEYWORDS, limit=10, text="")

    assert [h.payload["text"] for h in hits] == ["near", "far"]
    assert hits[0].score > hits[1].score


def test_a_missing_collection_is_an_empty_result(store):
    s, name = store
    assert s.search(name, [1.0, 0.0, 0.0], NO_KEYWORDS, limit=5, text="") == []


def test_an_upsert_of_an_existing_id_overwrites(store):
    ''' The property M3's embed retry depends on: a replay rewrites, never duplicates. '''
    s, name = store
    same_id = str(uuid.uuid4())
    s.upsert(name, [Point(same_id, [1.0, 0.0, 0.0], {"text": "first"})])
    s.upsert(name, [Point(same_id, [1.0, 0.0, 0.0], {"text": "second"})])

    assert s.client.count(name).count == 1
    [hit] = s.search(name, [1.0, 0.0, 0.0], NO_KEYWORDS, limit=5, text="")
    assert hit.payload["text"] == "second"


def test_hybrid_search_ranks_an_exact_token_match_first(store):
    ''' The keyword leg against real qdrant: IDF from the collection, RRF fusion. The
    dense leg alone would put "coverage" first. '''
    s, name = store
    s.upsert(
        name,
        [
            pt([1.0, 0.0, 0.0], "member coverage was reviewed"),
            pt([0.0, 1.0, 0.0], "appeal for member MBR-0048213"),
            pt([0.0, 0.0, 1.0], "unrelated fax cover sheet"),
        ],
    )

    hits = s.search(name, [1.0, 0.0, 0.0], bm25.encode_query("MBR-0048213"),
                    limit=3, text="MBR-0048213")

    assert hits[0].payload["text"] == "appeal for member MBR-0048213"


def test_ping_reaches_the_real_client_and_raises_when_it_does(monkeypatch):
    """/readyz relies on QdrantVectorStore.ping existing: the fake store in the health
    tests would pass even if the real class lacked it (it once did)."""
    import types

    import pytest

    from app.storage.vectors import QdrantVectorStore

    store = QdrantVectorStore("http://unused", 3)
    calls = []
    store._client = types.SimpleNamespace(get_collections=lambda: calls.append(1))
    store.ping()
    assert calls == [1]

    def down():
        raise ConnectionError("refused")

    store._client = types.SimpleNamespace(get_collections=down)
    with pytest.raises(ConnectionError):
        store.ping()


def test_search_retries_once_on_a_dropped_connection():
    import types

    import pytest
    from qdrant_client.http.exceptions import ResponseHandlingException

    from app.storage.vectors import QdrantVectorStore

    store = QdrantVectorStore("http://unused", 3)
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise ResponseHandlingException(ConnectionError("socket closed"))
        return []

    store._search = flaky
    assert store.search("c", [0.0, 0.0, 0.0], ([], []), 3, "") == []
    assert len(calls) == 2

    calls.clear()
    store._search = lambda *a, **kw: (_ for _ in ()).throw(
        ResponseHandlingException(ConnectionError("down"))
    )
    with pytest.raises(ResponseHandlingException):
        store.search("c", [0.0, 0.0, 0.0], ([], []), 3, "")  # a second failure surfaces
