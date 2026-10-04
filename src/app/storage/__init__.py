from functools import lru_cache

from app.config import get_settings
from app.storage.blob import BlobStore, LocalBlobStore
from app.storage.vectors import QdrantVectorStore, VectorStore


@lru_cache
def get_blob_store() -> BlobStore:
    s = get_settings()
    if s.blob_backend == "local":
        return LocalBlobStore(s.blob_local_root)
    raise ValueError(f"unknown blob_backend: {s.blob_backend!r}")


@lru_cache
def get_vector_store() -> VectorStore:
    # One impl, so no backend switch -- add one when a second store exists, not now.
    s = get_settings()
    return QdrantVectorStore(s.qdrant_url, s.embed_dim)
