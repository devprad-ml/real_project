''' AzureSearchVectorStore: the VectorStore on Azure AI Search. See m14.md.

One shared index for every tenant, unlike qdrant's collection per tenant (Azure caps
the number of indexes per service). So isolation is a filter, and the filter is this
class's job, not the caller's: every search and every write derives the tenant from the
collection name and applies it here. A collection name that is not `chunks_<uuid>`
raises before any request is made, so there is no code path that searches unfiltered.

Hybrid search is one request: BM25 over `content` plus a vector query over `vector`,
fused by Azure with RRF, the same fusion qdrant does. The sparse vector on a Point is
qdrant's; this store ignores it and indexes the chunk text instead.

Azure indexing is asynchronous: a chunk is searchable a short time after `upsert`
returns, where qdrant's `wait=True` makes it searchable at once.
'''

import uuid

from app.storage.vectors import PREFETCH_FACTOR, Hit, Point

VECTOR_FIELD = "vector"
# Fields returned by a search. `vector` is left out: 384 floats per hit nobody reads.
SELECT = ["id", "tenant_id", "document_id", "page_no", "doc_type", "content"]
# The service accepts 1000 documents or 16 MB per request; 500 chunks of a few hundred
# words plus a 384-float vector each stays well under both.
BATCH = 500
# One page of a delete sweep (the service's maximum for `top`).
DELETE_PAGE = 1000

_PREFIX = "chunks_"


def tenant_of(collection: str) -> str:
    ''' The tenant uuid inside `chunks_<uuid>`. Raises for anything else. The uuid is
    re-rendered from the parsed value, so what reaches a filter string is always 36
    hex-and-dash characters: no quote can get through. '''
    if not collection.startswith(_PREFIX):
        raise ValueError(f"not a tenant collection: {collection!r}")
    try:
        return str(uuid.UUID(collection[len(_PREFIX):]))
    except ValueError:
        raise ValueError(f"not a tenant collection: {collection!r}") from None


def _tenant_filter(tenant: str) -> str:
    return f"tenant_id eq '{tenant}'"


class AzureSearchVectorStore:
    def __init__(self, endpoint: str, api_key: str, dim: int, index: str = "chunks"):
        self.endpoint = endpoint
        self.api_key = api_key
        self.dim = dim
        self.index = index
        self._client = None
        self._index_client = None
        self._ensured = False

    @property
    def client(self):
        if self._client is None:
            from azure.core.credentials import AzureKeyCredential
            from azure.search.documents import SearchClient

            self._client = SearchClient(
                self.endpoint, self.index, AzureKeyCredential(self.api_key)
            )
        return self._client

    @property
    def index_client(self):
        if self._index_client is None:
            from azure.core.credentials import AzureKeyCredential
            from azure.search.documents.indexes import SearchIndexClient

            self._index_client = SearchIndexClient(
                self.endpoint, AzureKeyCredential(self.api_key)
            )
        return self._index_client

    def _ensure(self) -> None:
        ''' Create the index on first write. Only writers call this: a reader holds a
        data-reader role that cannot create indexes, and a missing index is already
        an empty result for it. Idempotent: create_or_update with the same definition
        changes nothing. '''
        if self._ensured:
            return
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            HnswParameters,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )

        string = SearchFieldDataType.String
        self.index_client.create_or_update_index(
            SearchIndex(
                name=self.index,
                fields=[
                    SimpleField(name="id", type=string, key=True),
                    SimpleField(name="tenant_id", type=string, filterable=True),
                    SimpleField(name="document_id", type=string, filterable=True),
                    SimpleField(name="page_no", type=SearchFieldDataType.Int32),
                    SimpleField(name="doc_type", type=string, filterable=True),
                    SearchableField(name="content", type=string),
                    SearchField(
                        name=VECTOR_FIELD,
                        type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                        searchable=True,
                        vector_search_dimensions=self.dim,
                        vector_search_profile_name="hnsw-cosine",
                    ),
                ],
                vector_search=VectorSearch(
                    profiles=[
                        VectorSearchProfile(
                            name="hnsw-cosine", algorithm_configuration_name="hnsw"
                        )
                    ],
                    algorithms=[
                        HnswAlgorithmConfiguration(
                            name="hnsw", parameters=HnswParameters(metric="cosine")
                        )
                    ],
                ),
            )
        )
        self._ensured = True

    def upsert(self, collection: str, points: list[Point]) -> None:
        tenant = tenant_of(collection)  # before any request
        if not points:
            return
        self._ensure()
        docs = [
            {
                "id": p.id,
                # The collection's tenant wins over whatever the payload says: a
                # point can only ever be written into the tenant it was addressed to.
                "tenant_id": tenant,
                "document_id": p.payload["document_id"],
                "page_no": p.payload["page_no"],
                "doc_type": p.payload["doc_type"],
                "content": p.payload["text"],
                VECTOR_FIELD: p.vector,
            }
            for p in points
        ]
        for start in range(0, len(docs), BATCH):
            results = self.client.merge_or_upload_documents(docs[start:start + BATCH])
            # A batch call succeeds as a whole while single documents inside it fail
            # (index busy, over a size limit). Treating that as success would mark the
            # document EMBEDDED with chunks missing; raise so the job retries.
            failed = [r.key for r in results if not r.succeeded]
            if failed:
                raise RuntimeError(
                    f"{len(failed)} of {len(results)} chunks were rejected by the index"
                )

    def search(
        self, collection: str, vector: list[float], sparse, limit: int, text: str
    ) -> list[Hit]:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.search.documents.models import VectorizedQuery

        tenant = tenant_of(collection)
        # Same as qdrant's PREFETCH_FACTOR: the vector leg looks wider than `limit` so a
        # chunk the keyword leg ranks first can still be fused in.
        query = VectorizedQuery(
            vector=vector,
            k_nearest_neighbors=limit * PREFETCH_FACTOR,
            fields=VECTOR_FIELD,
        )
        try:
            results = self.client.search(
                search_text=text or None,  # None: a query of no words is vector-only
                vector_queries=[query],
                filter=_tenant_filter(tenant),
                select=SELECT,
                top=limit,
            )
            rows = list(results)
        except ResourceNotFoundError:
            # The index appears with the first write; before it there is nothing.
            return []
        return [
            Hit(
                id=r["id"],
                score=r["@search.score"],
                payload={
                    "tenant_id": r["tenant_id"],
                    "document_id": r["document_id"],
                    "page_no": r["page_no"],
                    "doc_type": r["doc_type"],
                    "text": r["content"],
                },
            )
            for r in rows
        ]

    def drop(self, collection: str) -> None:
        ''' Delete this tenant's chunks. The service has no delete-by-query, so: page
        the ids, delete them, repeat. Stops when a page brings nothing new, because a
        document just deleted can show up once more before the index catches up. '''
        from azure.core.exceptions import ResourceNotFoundError

        tenant = tenant_of(collection)
        deleted: set[str] = set()
        try:
            while True:
                page = self.client.search(
                    search_text="*",
                    filter=_tenant_filter(tenant),
                    select=["id"],
                    top=DELETE_PAGE,
                )
                fresh = [r["id"] for r in page if r["id"] not in deleted]
                if not fresh:
                    return
                self.client.delete_documents([{"id": i} for i in fresh])
                deleted.update(fresh)
        except ResourceNotFoundError:
            return  # no index, so nothing to drop

    def ping(self) -> None:
        # Service-level, not index-level: the index does not exist before the first
        # write, and a fresh deployment is reachable, not down.
        self.index_client.get_service_statistics()
