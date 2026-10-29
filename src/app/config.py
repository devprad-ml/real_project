''' Application config file '''
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: SecretStr
    # Migrations run as an owner/superuser (CREATE TABLE, CREATE ROLE, GRANT);
    # database_url is the narrow app_user the API and worker run as. Unset in CI,
    # where the single DATABASE_URL already has the rights.
    alembic_database_url: SecretStr | None = None
    blob_backend: str = "local"   # local or AWS S3
    blob_local_root: str = "./_blobs"
    anthropic_api_key: SecretStr
    # Cheapest current model: picking one label from a fixed list doesn't need more.
    classify_model: str = "claude-haiku-5-5"
    # Leading characters sent to classify; the type is almost always on page one.
    classify_max_chars: int = Field(8000, ge=1)
    # Extraction has to hold several named fields plus a summary in mind at once --
    # real reading comprehension, not a single-label pick. Open to revisiting against
    # Haiku once accuracy is measured on real documents.
    extract_model: str = "claude-sonnet-5-5"
    # A field can be on any page, so extraction needs more of the document than
    # classify did.
    extract_max_chars: int = Field(20000, ge=1)
    # Haiku 5.5 at effort medium (answer.py), chosen on cost over Sonnet 5.5 (~12x)
    # after a live comparison (m12.md §8). Low effort left facts uncited.
    ask_model: str = "claude-haiku-5-5"
    # Chunks retrieved per question: ~a few thousand input tokens.
    ask_top_k: int = Field(8, ge=1, le=20)
    confidence_threshold: float = Field(0.85, ge=0.0, le=1.0)
    max_attempts: int = Field(5, ge=1)
    # Push waits out downstream outages: with the 15-min backoff cap and jitter, 30
    # attempts is 2.6-5.3 h of retrying before FAILED (m10.md §3).
    push_max_attempts: int = Field(30, ge=1)
    # A running job refreshes its lock every heartbeat, so a lock this stale means its
    # worker died. Kill-to-reclaim is at most lease + sweep = 45 s (m13.md §2.1).
    job_lease_seconds: int = Field(30, ge=1)
    job_heartbeat_seconds: int = Field(10, ge=1)
    sweep_seconds: int = Field(15, ge=1)
    # Load the embedder at API startup instead of on the first /ask (19 s, m12.md §7).
    warm_embedder: bool = True
    log_level: str = "INFO"
    # Threads that run sync routes (anyio's default is also 40). The DB pool is sized
    # from it in db.py: a request holds its connection while it waits for a thread, so
    # fewer connections than threads deadlocks under load (m13.md §8).
    threadpool_size: int = Field(40, ge=1)
    db_pool_size: int = Field(10, ge=1)
    # Requests over threadpool_size wait here, holding no thread and no connection; past
    # this they get a 503 instead of an unbounded queue.
    queue_timeout_seconds: float = Field(10, gt=0)
    # Which VectorStore the app uses: "qdrant" (local, the default) or "azure" (Azure AI
    # Search). Switching needs `python -m app.jobs.reindex`; the stores share no data.
    vector_backend: str = "qdrant"
    qdrant_url: str = "http://localhost:6333"
    azure_search_endpoint: str = ""
    azure_search_api_key: SecretStr | None = None
    azure_search_index: str = "chunks"
    # embed_dim must match embed_model's output width -- a collection is created with
    # a fixed vector size, so swapping the model means a new collection, not a reuse.
    embed_model: str = "all-MiniLM-L6-v2"
    embed_dim: int = Field(384, ge=1)
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: SecretStr | None = None
    imap_mailbox: str = "INBOX"
    imap_poll_seconds: int = Field(60, ge=1)
    # Messages fetched per poll. bulk=True keeps the batch in memory, so a backlog
    # needs a ceiling; the remainder waits for the next tick.
    imap_batch_size: int = Field(50, ge=1)
    # Cap at intake: the blob is never written, so an oversized attachment costs nothing.
    max_attachment_bytes: int = Field(25 * 1024 * 1024, ge=1)
    downstream_base_url: str  # str for now, change to HttpUrl later
    # Required: a missing secret must stop startup, never mean "accept unsigned".
    downstream_webhook_secret: SecretStr
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    return Settings()