''' QdrantVectorStore against a real qdrant. Skipped when none answers.

Every other test uses FakeVectorStore. This file is the check that the real store
agrees with the fake on the behaviours the pipeline depends on. Hand-written
three-dimensional vectors, no torch: this tests the store, not the model.
'''

import uuid

import pytest

from app.config import get_settings
from app.storage.vectors import Point, QdrantVectorStore


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
    return Point(id=str(uuid.uuid4()), vector=vector, payload={"text": text})


def test_the_nearest_point_comes_back_first_with_its_payload(store):
    s, name = store
    s.upsert(name, [pt([0.0, 1.0, 0.0], "far"), pt([1.0, 0.1, 0.0], "near")])

    hits = s.search(name, [1.0, 0.0, 0.0], limit=10)

    assert [h.payload["text"] for h in hits] == ["near", "far"]
    assert hits[0].score > hits[1].score


def test_a_missing_collection_is_an_empty_result(store):
    s, name = store
    assert s.search(name, [1.0, 0.0, 0.0], limit=5) == []


def test_an_upsert_of_an_existing_id_overwrites(store):
    ''' The property M3's embed retry depends on: a replay rewrites, never duplicates. '''
    s, name = store
    same_id = str(uuid.uuid4())
    s.upsert(name, [Point(same_id, [1.0, 0.0, 0.0], {"text": "first"})])
    s.upsert(name, [Point(same_id, [1.0, 0.0, 0.0], {"text": "second"})])

    assert s.client.count(name).count == 1
    [hit] = s.search(name, [1.0, 0.0, 0.0], limit=5)
    assert hit.payload["text"] == "second"
