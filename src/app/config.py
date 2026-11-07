''' Application config file '''
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: SecretStr
    # Migrations run as an owner/superuser (CREATE TABLE, CREATE ROLE, GRANT);
    # database_url is the narrow app_user the API runs as. Unset in CI,
    # where the single DATABASE_URL already has the rights.
    alembic_database_url: SecretStr | None = None
    blob_backend: str = "local"   # local or AWS S3
    blob_local_root: str = "./_blobs"
    anthropic_api_key: SecretStr
    # Cheapest current model: picking one label from a fixed list doesn't need more.
    classify_model: str = "claude-haiku-5-5"
    # Leading characters sent to classify; the type is almost always on page one.
    classify_max_chars: int = Field(8000, ge=1)
    # Haiku 5.5 at effort medium (answer.py), chosen on cost over Sonnet 5.5 (~12x)
    # after a live comparison (m12.md §8). Low effort left facts uncited.
    ask_model: str = "claude-haiku-5-5"
    # Chunks retrieved per question: ~a few thousand input tokens.
    ask_top_k: int = Field(8, ge=1, le=20)
    # Tries per document, one per imap_poll_seconds tick, before it stays failed.
    max_attempts: int = Field(5, ge=1)
    # Documents processed side by side. OCR is CPU-heavy, so keep this near the core count.
    ingest_workers: int = Field(4, ge=1)
    # The mailbox and retry timer. Off in tests.
    background_loop: bool = True
    # Load the embedder at API startup instead of on the first /ask (19 s, m12.md §7).
    warm_embedder: bool = True
    log_level: str = "INFO"
    # Tracing is off until both keys are set. EU accounts use the default URL, US accounts
    # https://us.cloud.langfuse.com. Capture sends question and document text to Langfuse,
    # so leave it off for anything but synthetic data unless there is a BAA.
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_capture_content: bool = False
    # Threads that run sync routes (anyio's default is also 40). The DB pool is sized
    # from it in db.py: a request holds its connection while it waits for a thread, so
    # fewer connections than threads deadlocks under load (m13.md §8).
    threadpool_size: int = Field(40, ge=1)
    db_pool_size: int = Field(10, ge=1)
    # Requests over threadpool_size wait here, holding no thread and no connection; past
    # this they get a 503 instead of an unbounded queue.
    queue_timeout_seconds: float = Field(10, gt=0)
    # Which VectorStore the app uses: "qdrant" (local, the default) or "azure" (Azure AI
    # Search). Switching needs `python -m app.ingestion.reindex`; the stores share no data.
    vector_backend: str = "qdrant"
    qdrant_url: str = "http://localhost:6333"
    azure_search_endpoint: str = ""
    azure_search_api_key: SecretStr | None = None
    azure_search_index: str = "chunks"
    # embed_dim must match embed_model's output width -- a collection is created with
    # a fixed vector size, so swapping the model means a new collection, not a reuse.
    embed_model: str = "all-MiniLM-L6-v2"
    embed_dim: int = Field(384, ge=1)
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: SecretStr | None = None
    imap_mailbox: str = "INBOX"
    imap_poll_seconds: int = Field(60, ge=1)
    # Messages fetched per poll. bulk=True keeps the batch in memory, so a backlog
    # needs a ceiling; the remainder waits for the next tick.
    imap_batch_size: int = Field(50, ge=1)
    # Floor on how far back a poll will look, in days. Without this, wiring a new
    # tenant to a mailbox that already has years of unread mail tries to ingest all
    # of it as documents. 0 disables the floor (fetch every unseen message, however
    # old).
    imap_lookback_days: int = Field(3, ge=0)
    # Cap at intake: the blob is never written, so an oversized attachment costs nothing.
    max_attachment_bytes: int = Field(25 * 1024 * 1024, ge=1)
    # utf-8-sig: some Windows editors save a byte-order mark, which would otherwise become
    # part of the first setting's name and make it look missing.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8-sig", extra="ignore"
    )

@lru_cache
def get_settings() -> Settings:
    return Settings()