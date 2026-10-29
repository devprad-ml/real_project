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
    s = get_settings()
    if s.vector_backend == "qdrant":
        return QdrantVectorStore(s.qdrant_url, s.embed_dim)
    if s.vector_backend == "azure":
        if not (s.azure_search_endpoint and s.azure_search_api_key):
            raise ValueError("vector_backend=azure needs the endpoint and the api key")
        from app.storage.azure_search import AzureSearchVectorStore

        return AzureSearchVectorStore(
            s.azure_search_endpoint,
            s.azure_search_api_key.get_secret_value(),
            s.embed_dim,
            s.azure_search_index,
        )
    raise ValueError(f"unknown vector_backend: {s.vector_backend!r}")


def move_normalized(session, doc, pdfa: bytes) -> None:
    ''' Re-file the normalized PDF under its current `doc_type`. Write the new key,
    point the row at it, and delete the old key only once the row change commits: a
    crash at any step leaves the row pointing at a blob that exists. Used by classify
    and by a reviewer's reclassify. '''
    from app.storage.blob import delete_after_commit, normalized_key

    old = doc.normalized_blob_key
    new = normalized_key(doc.tenant_id, doc.id, doc.doc_type)
    if new == old:
        return
    blob = get_blob_store()
    blob.put(new, pdfa, "application/pdf")
    doc.normalized_blob_key = new
    delete_after_commit(session, blob, old)
