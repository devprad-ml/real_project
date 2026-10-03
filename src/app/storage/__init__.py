from functools import lru_cache

from app.config import get_settings
from app.storage.blob import BlobStore, LocalBlobStore


@lru_cache
def get_blob_store() -> BlobStore:
    s = get_settings()
    if s.blob_backend == "local":
        return LocalBlobStore(s.blob_local_root)
    raise ValueError(f"unknown blob_backend: {s.blob_backend!r}")
